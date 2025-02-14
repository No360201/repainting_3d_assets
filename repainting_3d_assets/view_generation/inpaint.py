import copy
import json

import cv2
import numpy as np
import torch
from PIL import ImageOps, Image
from pytorch3d.io import load_ply
from pytorch3d.structures import Meshes
from pytorch3d.renderer.mesh import TexturesUV

from repainting_3d_assets.view_generation.mask_operations import (
    mask_proc_options,
    mask_ops,
)
from repainting_3d_assets.view_generation.pt3d_mesh_io import load_obj
from repainting_3d_assets.view_generation.reproj import (
    render_depth_map,
    backward_oculusion_aware_render,
    render_img,
)
from repainting_3d_assets.view_generation.utils import (
    import_config_key,
    create_dir,
    listify_matrix,
)
from repainting_3d_assets.view_generation.utils3D import (
    swap_faces,
    position_verts,
    init_ngp_config,
    write_outframe,
    convert_pt_NGP_transform,
)


def view_dep_prompt(prompt, angle, color=""):
    if color == "":
        base_prompt = f"A photo of a {prompt}"
    else:
        base_prompt = f"A photo of a {color} {prompt}"
    if angle <= 45 or angle >= 315:
        return f"{base_prompt}, front view"
    if 45 <= angle <= 135:
        return f"{base_prompt}, left view"
    if 135 <= angle <= 225:
        return f"{base_prompt}, back view"
    if 225 <= angle <= 315:
        return f"{base_prompt}, right view"
    return prompt


def initialize_meshes(inpaint_config, mesh_config, device):
    swap_face = import_config_key(inpaint_config, "swap_face", False)
    trans_mat = import_config_key(mesh_config, "trans_mat", torch.eye(3))
    mesh_path = import_config_key(mesh_config, "obj", "")

    if mesh_path[-3:] == "obj":
        verts, faces, aux = load_obj(
            mesh_path,
            load_textures=True,
            create_texture_atlas=False,
            swap_face=swap_face,
        )
        # faces = faces.verts_idx
    elif mesh_path[-3:] == "ply":
        verts, faces = load_ply(mesh_path)
        if swap_face:
            faces = swap_faces(faces)
    else:
        raise ValueError("Expecting obj or ply")

    # verts = position_verts(verts, trans_mat, swap_face=swap_face)
    # meshes = Meshes(verts=[verts], faces=[faces]).to(device)

    # TexturesUV type
    tex_maps = aux.texture_images
    if tex_maps is not None and len(tex_maps) > 0:
        verts_uvs = aux.verts_uvs.to(device)  # (V, 2)
        faces_uvs = faces.textures_idx.to(device)  # (F, 3)
        image = list(tex_maps.values())[0].to(device)[None]
        tex = TexturesUV(
            verts_uvs=[verts_uvs], faces_uvs=[faces_uvs], maps=image
        )
    faces = faces.verts_idx
    verts = position_verts(verts, trans_mat, swap_face=swap_face)
    meshes = Meshes(verts=[verts], faces=[faces],  textures=tex).to(device)

    return meshes


def inpaint_first_view(meshes, pipe, latents, inpaint_config, mesh_config, device):
    num_inference_steps = inpaint_config["num_inference_steps"]
    save_dir = create_dir(mesh_config["save_dir"])
    dataset_dir = create_dir(f"{save_dir}/dataset")
    transforms_config_out = init_ngp_config(inpaint_config)
    n_propmt = import_config_key(inpaint_config, "negative_prompt", None)
    prompt_obj = mesh_config["prompt"]
    color_obj = import_config_key(mesh_config, "color", "")
    input_img_path = import_config_key(inpaint_config, "input_img_path", "")

    init_angle = 0
    cur_angle = init_angle
    next_angle = cur_angle

    input_image = render_img(
        cur_angle, meshes, inpaint_config, mesh_config, device = device
    )
    depth_path = render_depth_map(
        cur_angle, meshes, inpaint_config, mesh_config, device
    )
    d = np.load(depth_path)
    d = torch.tensor(d).float().to(device)
    depth_tensor = d
    depth_tensor = (depth_tensor.max() - depth_tensor).float()
    depth_tensor = depth_tensor / (depth_tensor.max())
    # if input_img_path == "":
    #     input_image = Image.fromarray((255 * np.ones((512, 512, 3))).astype(np.uint8))
    # else:
    #     input_image = Image.open(input_img_path).convert("RGB")
    mask = Image.fromarray((255 * np.ones((512, 512, 3))).astype(np.uint8))

    image = pipe(
        prompt=view_dep_prompt(prompt_obj, next_angle, color_obj),
        image=input_image,
        mask_image=mask,
        depth_map=depth_tensor[None, :, :],
        negative_prompt=n_propmt,
        strength=1,
        num_inference_steps=num_inference_steps,
        latents=latents,
        inpainting_strength=0,
        desc="Generating style",
    ).images[0]
    i = cur_angle
    ipt_save_dir = create_dir(f"{dataset_dir}/{i}")
    input_image.save(f"{ipt_save_dir}/input_image.png")
    image.save(f"{ipt_save_dir}/out.png")
    image = np.array(image.convert("RGBA"))

    if np.all(image[:, :, :3] == np.zeros_like(image[:, :, :3])):
        image = 255 * np.ones_like(image)

    d = np.load(depth_path)
    image[:, :, 3] = (d != d.max()).astype("uint8") * 255

    image_masked = np.where(
        image[:, :, 3:4] == 255, image[:, :, :3], np.zeros_like(image[:, :, :3])
    )
    image_masked = image_masked.astype("float32")
    img_color = np.sum(image_masked, axis=(0, 1))
    if (np.sum(image[:, :, 3])) > 0:
        img_color = img_color / (np.sum(image[:, :, 3]))
    else:
        img_color = np.ones_like(img_color)

    img_color = np.repeat(img_color[np.newaxis, :], 512, axis=0)
    img_color = np.repeat(img_color[np.newaxis, :], 512, axis=0)
    image[:, :, :3] = np.where(
        image[:, :, 3:4] == 255, image[:, :, :3], (255 * img_color).astype("uint8")
    )

    # input_image = Image.fromarray((255 * img_color).astype("uint8"))

    bg_image = torch.tensor(img_color, device=device).float()
    image = pipe(
        prompt=view_dep_prompt(prompt_obj, next_angle, color_obj),
        image=input_image,
        mask_image=mask,
        depth_map=depth_tensor[None, :, :],
        negative_prompt=n_propmt,
        strength=1,
        num_inference_steps=num_inference_steps,
        latents=latents,
        inpainting_strength=0,
        desc="Painting front view",
    ).images[0]
    image.save(f"{ipt_save_dir}/out.png")
    image = np.array(image.convert("RGBA"))

    if np.all(image[:, :, :3] == np.zeros_like(image[:, :, :3])):
        image = np.random.randint(0, high=255, size=image.shape).astype(np.uint8)
        image = Image.fromarray(image.astype("uint8"))
        image.save(f"{ipt_save_dir}/out_alpha.png")
        image.save(f"{ipt_save_dir}/out.png")
    else:
        d = np.load(depth_path)
        image[:, :, 3] = (d != d.max()).astype("uint8") * 255
        image = Image.fromarray(image)
        image.save(f"{ipt_save_dir}/out_alpha.png")

    transforms_config_out = write_outframe(
        next_angle, ipt_save_dir, transforms_config_out, save_dir
    )

    return transforms_config_out, bg_image


def inpaint_new_angle(
    next_angle,
    inc_total,
    inpaint_config,
    mesh_config,
    pipe,
    latents,
    meshes,
    bg_image,
    transforms_config_out,
    angle_inc,
    device,
):
    num_inference_steps = inpaint_config["num_inference_steps"]
    save_dir = create_dir(mesh_config["save_dir"])
    dataset_dir = create_dir(f"{save_dir}/dataset")
    n_propmt = import_config_key(inpaint_config, "negative_prompt", None)
    inpainting_strength = import_config_key(inpaint_config, "inpainting_strength", 1)
    prompt_obj = mesh_config["prompt"]
    color_obj = import_config_key(mesh_config, "color", "")
    mask_operation = inpaint_config["mask_blend"]
    mask_blend_kernel = import_config_key(inpaint_config, "mask_blend_kernel", -1)
    latent_blend_kernel = import_config_key(inpaint_config, "latent_blend_kernel", -1)

    cur_angle = next_angle
    next_angle = (cur_angle + angle_inc) % 360
    i = next_angle

    images = backward_oculusion_aware_render(
        cur_angle,
        next_angle,
        inpaint_config,
        mesh_config,
        meshes,
        bg_image,
        angle_inc=angle_inc,
        use_train=False,
        device=device,
    )

    inc_total = inc_total + np.abs(angle_inc)
    ipt_input_dir = create_dir(f"{dataset_dir}/{i}")
    ipt_save_dir = create_dir(f"{dataset_dir}/{i}")

    depth_path = render_depth_map(
        next_angle, meshes, inpaint_config, mesh_config, device
    )

    render_uint8 = (images[0, :, :, :3].cpu().numpy() * 255).astype(np.uint8)

    render_uint8_img = Image.fromarray(render_uint8)

    img_path = f"{ipt_input_dir}/rgb.png"
    # import pdb
    # pdb.set_trace()
    d = torch.tensor(np.load(depth_path)).to(device)

    images[0, :, :, 3] = torch.where(
        (d == d.max()), torch.ones_like(images[0, :, :, 3]), images[0, :, :, 3]
    )

    render_uint8_img.save(img_path)

    mask = images[0][:, :, 3].cpu().numpy()

    mask_uint8 = (mask * 255).astype(np.uint8)
    mask = mask_proc_options[mask_operation](mask_uint8, kernel_size=5)
    mask_path = f"{ipt_input_dir}/mask_{mask_ops[mask_operation]}.png"
    cv2.imwrite(mask_path, mask)

    input_image_coarse = render_img(
        next_angle, meshes, inpaint_config, mesh_config, device = device
    )
    mask_path = f"{ipt_input_dir}/mask_coarse.png"
    input_image_coarse.save(mask_path)
    # cv2.imwrite(mask_path, input_image_coarse)

    input_image = Image.open(img_path)
    mask_tmp = images[0][:, :, 3].cpu().numpy()[:,:,None].repeat(3,2)
    import pdb
    pdb.set_trace()
    input_image = input_image * mask_tmp + (1 - mask_tmp) * cv2.cvtColor(np.asarray(input_image_coarse),cv2.COLOR_RGB2BGR)  
    
    mask_path = f"{ipt_input_dir}/mask_together.png"
    input_image.save(mask_path)
    cv2.imwrite(mask_path, input_image)

    mask = Image.open(mask_path)
    mask = mask.convert("L")

    mask = ImageOps.invert(mask)

    input_image.save(f"{ipt_save_dir}/preproc.png")

    import pdb
    pdb.set_trace()

    d = np.load(depth_path)
    d = torch.tensor(d).float().to(device)
    depth_tensor = d
    depth_tensor = (depth_tensor.max() - depth_tensor).float()
    depth_tensor = depth_tensor / (depth_tensor.max())
    image = pipe(
        prompt=view_dep_prompt(prompt_obj, next_angle, color_obj),
        image=input_image,
        mask_image=mask,
        depth_map=depth_tensor[None, :, :],
        negative_prompt=n_propmt,
        strength=1,
        num_inference_steps=num_inference_steps,
        latents=latents,
        inpainting_strength=inpainting_strength,
        mask_blend_kernel=mask_blend_kernel,
        latent_blend_kernel=latent_blend_kernel,
        desc=f"Inpainting {next_angle} deg. view",
    ).images[0]

    image.save(f"{ipt_save_dir}/out.png")
    image = np.array(image.convert("RGBA"))

    if np.all(image[:, :, :3] == np.zeros_like(image[:, :, :3])):
        image = np.random.randint(0, high=255, size=image.shape).astype(np.uint8)
        image = Image.fromarray(image.astype("uint8"))
        image.save(f"{ipt_save_dir}/out_alpha.png")
        image.save(f"{ipt_save_dir}/out.png")
    else:
        d = np.load(depth_path)
        image[:, :, 3] = (d != d.max()).astype("uint8") * 255
        image = Image.fromarray(image)
        image.save(f"{ipt_save_dir}/out_alpha.png")

    transforms_config_out = write_outframe(
        next_angle, ipt_save_dir, transforms_config_out, save_dir
    )

    return cur_angle, next_angle, inc_total, transforms_config_out


def inpaint_bidirectional(
    view_1,
    view_2,
    view_synth,
    inpaint_config,
    mesh_config,
    pipe,
    latents,
    meshes,
    transforms_config_out,
    bg_image,
    device,
):
    num_inference_steps = inpaint_config["num_inference_steps"]
    save_dir = create_dir(mesh_config["save_dir"])
    dataset_dir = create_dir(f"{save_dir}/dataset")
    n_propmt = import_config_key(inpaint_config, "negative_prompt", None)
    inpainting_strength = import_config_key(inpaint_config, "inpainting_strength", 1)
    prompt_obj = mesh_config["prompt"]
    color_obj = import_config_key(mesh_config, "color", "")
    mask_option = inpaint_config["mask_blend"]
    mask_blend_kernel = import_config_key(inpaint_config, "mask_blend_kernel", -1)
    latent_blend_kernel = import_config_key(inpaint_config, "latent_blend_kernel", -1)

    i = view_synth
    ipt_save_dir = create_dir(f"{dataset_dir}/{i}")

    images2 = backward_oculusion_aware_render(
        view_2,
        view_synth,
        inpaint_config,
        mesh_config,
        meshes,
        bg_image,
        angle_inc=view_synth - view_2,
        use_train=True,
        device=device,
    )

    render_uint8 = (images2[0, :, :, :3].cpu().numpy() * 255).astype(np.uint8)

    render_uint8_img = Image.fromarray(render_uint8)
    img_path = f"{ipt_save_dir}/rgb_{view_2}.png"
    render_uint8_img.save(img_path)

    images = backward_oculusion_aware_render(
        view_1,
        view_synth,
        inpaint_config,
        mesh_config,
        meshes,
        bg_image,
        angle_inc=view_synth - view_1,
        use_train=True,
        device=device,
    )

    render_uint8 = (images[0, :, :, :3].cpu().numpy() * 255).astype(np.uint8)

    render_uint8_img = Image.fromarray(render_uint8)
    img_path = f"{ipt_save_dir}/rgb_{view_1}.png"
    render_uint8_img.save(img_path)

    images_combined = torch.ones_like(images[0])
    mask_pt = images[0, :, :, 3:4] >= 0.5
    mask_pt2 = images2[0, :, :, 3:4] >= 0.5

    images_combined = torch.where(mask_pt, images[0], images_combined)
    images_combined = torch.where(mask_pt2, images2[0], images_combined)
    images_combined[:, :, 3:4] = torch.maximum(
        (images[0, :, :, 3:4]), (images2[0, :, :, 3:4])
    )

    mask_combined = torch.maximum((images[0, :, :, 3:4]), (images2[0, :, :, 3:4]))
    mask_blend = torch.logical_xor(mask_pt, mask_pt2)[:, :, 0]

    bg_image_np = bg_image.cpu().numpy()
    bg_image_np = (bg_image_np * 255).astype("uint8")
    images_combined = (255 * images_combined.cpu().numpy()).astype("uint8")
    mask_combined = (255 * mask_combined.cpu().numpy()).astype("uint8")
    images_combined[:, :, :3] = np.where(
        mask_combined, images_combined[:, :, :3], bg_image_np
    )
    images_combined_pil = Image.fromarray(images_combined[:, :, :3])

    img_path_ngp = f"{dataset_dir}/{view_synth}/out_train.png"
    depth_path_ngp = render_depth_map(
        view_synth, meshes, inpaint_config, mesh_config, device
    )

    d = np.load(depth_path_ngp)
    depth_tensor = torch.tensor(d).float().to(device)
    depth_tensor = torch.where(
        depth_tensor == depth_tensor.max(), depth_tensor, depth_tensor
    )
    depth_tensor = (depth_tensor.max() - depth_tensor).float()

    img_ngp = np.array(Image.open(img_path_ngp))
    img_ngp[:, :, :3] = np.where(
        (d == d.max())[:, :, np.newaxis], bg_image_np, img_ngp[:, :, :3]
    )
    img_ngp_pil = Image.fromarray(img_ngp[:, :, :3])

    mask_combined = torch.maximum(
        torch.maximum((images[0, :, :, 3]), (images2[0, :, :, 3])),
        torch.tensor(d == d.max()).to(device),
    )

    mask_pil = Image.fromarray((255 * mask_combined.cpu().numpy()).astype("uint8"))

    mask_uint8 = np.array(mask_pil)

    mask = mask_proc_options[mask_option](mask_uint8, kernel_size=5)
    mask = Image.fromarray(mask)
    mask = mask.convert("L")  # binarize and single channel

    mask_blend = torch.maximum(mask_blend, torch.tensor(d == d.max()).to(device))
    mask_blend = Image.fromarray((255 * mask_blend.cpu().numpy()).astype("uint8"))

    image_pil = Image.composite(images_combined_pil, img_ngp_pil, mask_blend)  # blend

    mask = mask.convert("L")

    mask = ImageOps.invert(mask)

    image_pil.save(f"{ipt_save_dir}/input.png")
    image = pipe(
        view_dep_prompt(prompt_obj, view_synth, color_obj),
        image=image_pil,
        mask_image=mask,
        depth_map=depth_tensor[None, :, :],
        negative_prompt=n_propmt,
        strength=1,
        num_inference_steps=num_inference_steps,
        latents=latents,
        inpainting_strength=inpainting_strength,
        mask_blend_kernel=mask_blend_kernel,
        latent_blend_kernel=latent_blend_kernel,
        desc=f"Inpainting {view_synth} deg. view",
    ).images[0]

    image.save(f"{ipt_save_dir}/out.png")
    image = np.array(image.convert("RGBA"))
    if np.all(image[:, :, :3] == np.zeros_like(image[:, :, :3])):
        image = np.random.randint(0, high=255, size=image.shape).astype(np.uint8)
        image = Image.fromarray(image.astype("uint8"))
        image.save(f"{ipt_save_dir}/out_alpha.png")
        image.save(f"{ipt_save_dir}/out.png")
    else:
        d = np.load(depth_path_ngp)
        image[:, :, 3] = (d != d.max()).astype("uint8") * 255
        image = Image.fromarray(image)
        image.save(f"{ipt_save_dir}/out_alpha.png")
    transforms_config_out = write_outframe(
        view_synth, ipt_save_dir, transforms_config_out, save_dir
    )

    return view_1, view_2, view_synth, transforms_config_out


def write_train_transforms(
    view_1, view_2, view_synth, mesh_config, transforms_config_out, device
):
    save_dir = create_dir(mesh_config["save_dir"])
    dataset_dir = create_dir(f"{save_dir}/dataset")
    transforms_config_train = copy.deepcopy(transforms_config_out)

    def train_outframe(angle):
        file_dir = f"./dataset/{angle}/"
        return {
            "file_dir": file_dir,
            "transform_matrix": listify_matrix(
                convert_pt_NGP_transform(
                    torch.tensor([0]).to(device), torch.tensor([angle]).to(device)
                )
            )[0],
        }

    transforms_config_train["frames"] = [
        train_outframe(view_1),
        train_outframe(view_2),
        train_outframe(view_synth),
    ]
    with open(f"{dataset_dir}/train_transforms.json", "w") as out_file:
        json.dump(transforms_config_train, out_file, indent=4)


def inpaint_facade(
    inpaint_config,
    mesh_config,
    pipe,
    latents,
    meshes,
    bg_image,
    transforms_config_out,
    device,
):
    angle_inc = import_config_key(inpaint_config, "angle_inc", 40)
    inc_limit = import_config_key(inpaint_config, "inc_limit", 120)
    init_angle = 0
    inc_total = 0
    next_angle = init_angle
    while True:
        _, next_angle, inc_total, transforms_config_out = inpaint_new_angle(
            next_angle,
            inc_total,
            inpaint_config,
            mesh_config,
            pipe,
            latents,
            meshes,
            bg_image,
            transforms_config_out,
            angle_inc,
            device,
        )
        if inc_total >= inc_limit:
            break
    view_1 = (360 + inc_total + init_angle) % 360

    inc_total = 0
    next_angle = init_angle
    while True:
        _, next_angle, inc_total, transforms_config_out = inpaint_new_angle(
            next_angle,
            inc_total,
            inpaint_config,
            mesh_config,
            pipe,
            latents,
            meshes,
            bg_image,
            transforms_config_out,
            -angle_inc,
            device,
        )

        if inc_total >= inc_limit:
            break

    view_2 = (360 - inc_total + init_angle) % 360

    return view_1, view_2, transforms_config_out
